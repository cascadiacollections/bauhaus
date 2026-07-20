"""Image quality scoring for source filtering and output evaluation.

Lightweight quality metrics evaluated on CPU using Pillow and NumPy.
Used to reject low-quality source images before style transfer and to
provide a cheap aesthetic signal for generated outputs.
"""

import numpy as np
from PIL import Image, ImageFilter

# Minimum acceptable image dimensions (width or height).
MIN_DIMENSION = 512

# Minimum sharpness score (Laplacian variance).  Images below this
# threshold are likely too blurry to produce good stylized output.
MIN_SHARPNESS = 100.0

# Acceptable aspect-ratio range (width / height).
# Extremely tall or wide images don't look good as wallpapers.
MIN_ASPECT_RATIO = 0.5   # 1:2 portrait
MAX_ASPECT_RATIO = 3.0   # 3:1 panoramic


def sharpness_score(image: Image.Image) -> float:
    """Estimate image sharpness via Laplacian-like edge energy.

    Applies a 3×3 edge-detection kernel and returns the variance of the
    result.  Higher values indicate sharper images.

    The image is down-sampled to 512px (longest side) before scoring to
    keep computation fast and resolution-independent.  Border pixels
    (which produce artifacts from the convolution kernel) are excluded.
    """
    img = image.convert("L")

    # Down-sample for consistent, fast scoring
    w, h = img.size
    scale = min(512 / max(w, h), 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    edges = img.filter(ImageFilter.FIND_EDGES)

    # Crop 2px border to avoid convolution boundary artifacts
    ew, eh = edges.size
    if ew > 4 and eh > 4:
        edges = edges.crop((2, 2, ew - 2, eh - 2))

    arr = np.asarray(edges, dtype=np.float32)
    if arr.size == 0:
        return 0.0
    return float(arr.var())


def check_resolution(image: Image.Image, min_dim: int = MIN_DIMENSION) -> bool:
    """Return True if both dimensions meet the minimum threshold."""
    w, h = image.size
    return w >= min_dim and h >= min_dim


def check_aspect_ratio(
    image: Image.Image,
    min_ratio: float = MIN_ASPECT_RATIO,
    max_ratio: float = MAX_ASPECT_RATIO,
) -> bool:
    """Return True if the aspect ratio is within an acceptable range."""
    w, h = image.size
    if h == 0:
        return False
    ratio = w / h
    return min_ratio <= ratio <= max_ratio


def colorfulness_score(image: Image.Image) -> float:
    """Estimate color richness using a lightweight RGB variance heuristic."""
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    r = rgb[..., 0]
    g = rgb[..., 1]
    b = rgb[..., 2]

    rg = r - g
    yb = 0.5 * (r + g) - b
    colorfulness = np.sqrt(np.mean(rg * rg) + np.mean(yb * yb))
    return float(colorfulness)


def contrast_score(image: Image.Image) -> float:
    """Estimate perceptual contrast from grayscale standard deviation."""
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    return float(gray.std())


def aesthetic_score(image: Image.Image) -> dict:
    """Create a lightweight 0–10 aesthetic signal from CPU-only heuristics."""
    sharpness = sharpness_score(image)
    colorfulness = colorfulness_score(image)
    contrast = contrast_score(image)

    sharp_norm = min(1.0, sharpness / 500.0)
    color_norm = min(1.0, colorfulness / 80.0)
    contrast_norm = min(1.0, contrast / 80.0)
    score = 10.0 * (0.45 * sharp_norm + 0.30 * color_norm + 0.25 * contrast_norm)

    return {
        "score": round(float(score), 2),
        "sharpness": round(float(sharpness), 2),
        "colorfulness": round(float(colorfulness), 2),
        "contrast": round(float(contrast), 2),
        "method": "heuristic-v1",
    }


def score_image(image: Image.Image) -> dict:
    """Compute all quality metrics for an image.

    Returns a dict with individual scores and an overall ``pass`` bool.
    """
    w, h = image.size
    sharpness = sharpness_score(image)
    res_ok = check_resolution(image)
    ar_ok = check_aspect_ratio(image)
    sharp_ok = sharpness >= MIN_SHARPNESS

    return {
        "width": w,
        "height": h,
        "sharpness": round(sharpness, 2),
        "resolution_ok": res_ok,
        "aspect_ratio_ok": ar_ok,
        "sharpness_ok": sharp_ok,
        "pass": res_ok and ar_ok and sharp_ok,
    }
