"""Image quality scoring for source filtering.

Evaluates fetched images on resolution, aspect ratio, and sharpness
to reject low-quality sources before stylization.
"""

from io import BytesIO

from PIL import Image, ImageFilter, ImageStat

# Minimum width or height in pixels.
MIN_DIMENSION = 512

# Maximum aspect ratio (long side / short side).
MAX_ASPECT_RATIO = 3.0

# Minimum sharpness score (Laplacian variance).
# Conservative threshold — rejects only very blurry images.
MIN_SHARPNESS = 50.0


def compute_sharpness(image: Image.Image) -> float:
    """Compute sharpness via Laplacian variance.

    Converts image to grayscale, applies a Laplacian filter,
    and returns the variance of the result.  Higher values
    indicate sharper images.

    Args:
        image: Input PIL Image.

    Returns:
        Laplacian variance (float).  Typical ranges:
        < 50 — very blurry, 50-200 — moderate, 200+ — sharp.
    """
    gray = image.convert("L")
    laplacian = gray.filter(ImageFilter.Kernel(
        size=(3, 3),
        kernel=[-1, -1, -1, -1, 8, -1, -1, -1, -1],
        scale=1,
        offset=128,
    ))
    stat = ImageStat.Stat(laplacian)
    return stat.var[0]


def check_resolution(width: int, height: int, min_dim: int = MIN_DIMENSION) -> bool:
    """Return True if both dimensions meet the minimum."""
    return width >= min_dim and height >= min_dim


def check_aspect_ratio(
    width: int, height: int, max_ratio: float = MAX_ASPECT_RATIO,
) -> bool:
    """Return True if aspect ratio is within acceptable range."""
    if width <= 0 or height <= 0:
        return False
    ratio = max(width, height) / min(width, height)
    return ratio <= max_ratio


def passes_quality_gate(
    image_bytes: bytes,
    *,
    min_dimension: int = MIN_DIMENSION,
    max_aspect_ratio: float = MAX_ASPECT_RATIO,
    min_sharpness: float = MIN_SHARPNESS,
) -> tuple[bool, str]:
    """Check if an image passes quality requirements.

    Args:
        image_bytes: Raw image bytes (JPEG, PNG, etc.).
        min_dimension: Minimum width and height in pixels.
        max_aspect_ratio: Maximum long/short side ratio.
        min_sharpness: Minimum Laplacian variance.

    Returns:
        Tuple of (passed, reason).  If passed is True, reason is empty.
        If passed is False, reason describes why the image was rejected.
    """
    try:
        img = Image.open(BytesIO(image_bytes))
    except Exception:
        return False, "could not decode image"

    width, height = img.size

    if not check_resolution(width, height, min_dimension):
        return False, f"resolution too low ({width}x{height}, min {min_dimension})"

    if not check_aspect_ratio(width, height, max_aspect_ratio):
        ratio = max(width, height) / min(width, height)
        return False, f"aspect ratio too extreme ({ratio:.1f}:1, max {max_aspect_ratio}:1)"

    sharpness = compute_sharpness(img.convert("RGB"))
    if sharpness < min_sharpness:
        return False, f"too blurry (sharpness={sharpness:.1f}, min {min_sharpness})"

    return True, ""
