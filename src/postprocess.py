"""Post-processing pipeline for stylized images.

Applies color harmonization, sharpening, and optional super-resolution
to improve quality after AdaIN style transfer.
"""

from PIL import Image, ImageFilter


def _cumulative_sum(hist: list[int]) -> list[int]:
    """Compute cumulative sum of a histogram."""
    cdf: list[int] = []
    running = 0
    for val in hist:
        running += val
        cdf.append(running)
    return cdf


def _build_histogram_lut(source_hist: list[int], reference_hist: list[int]) -> list[int]:
    """Build a lookup table to match source histogram to reference histogram."""
    src_cdf = _cumulative_sum(source_hist)
    ref_cdf = _cumulative_sum(reference_hist)

    src_total = src_cdf[-1] or 1
    ref_total = ref_cdf[-1] or 1

    lut: list[int] = []
    for src_val in range(256):
        target = src_cdf[src_val] / src_total
        best = 255
        for ref_val in range(256):
            if ref_cdf[ref_val] / ref_total >= target:
                best = ref_val
                break
        lut.append(best)

    return lut


def color_harmonize(
    stylized: Image.Image,
    content: Image.Image,
    strength: float = 0.8,
) -> Image.Image:
    """Histogram-match stylized image to content image to reduce color shifts.

    Args:
        stylized: The stylized output image.
        content: The original content image (reference for color distribution).
        strength: Blend factor 0.0 (no change) to 1.0 (full histogram match).

    Returns:
        Color-harmonized image.
    """
    if strength <= 0.0:
        return stylized

    stylized_rgb = stylized.convert("RGB")
    content_rgb = content.convert("RGB")

    # Resize content to match stylized dimensions for histogram comparison
    if content_rgb.size != stylized_rgb.size:
        content_rgb = content_rgb.resize(stylized_rgb.size, Image.LANCZOS)

    # Per-channel histogram matching
    s_channels = stylized_rgb.split()
    c_channels = content_rgb.split()

    matched_channels: list[Image.Image] = []
    for s_ch, c_ch in zip(s_channels, c_channels):
        lut = _build_histogram_lut(s_ch.histogram(), c_ch.histogram())
        matched_channels.append(s_ch.point(lut))

    matched = Image.merge("RGB", matched_channels)

    if strength >= 1.0:
        return matched

    return Image.blend(stylized_rgb, matched, strength)


def sharpen(
    image: Image.Image,
    radius: float = 2.0,
    percent: int = 150,
    threshold: int = 3,
) -> Image.Image:
    """Apply unsharp mask sharpening to enhance detail.

    Args:
        image: Input image.
        radius: Blur radius for unsharp mask.
        percent: Sharpening strength percentage.
        threshold: Minimum brightness difference to sharpen.

    Returns:
        Sharpened image.
    """
    return image.filter(
        ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=threshold),
    )


def upscale(image: Image.Image, scale: int = 2) -> Image.Image:
    """Apply super-resolution upscaling.

    Attempts to use Real-ESRGAN if available, falls back to high-quality
    LANCZOS resampling.  Install ``realesrgan`` for neural upscaling::

        pip install realesrgan

    Args:
        image: Input image.
        scale: Upscaling factor (default: 2x).

    Returns:
        Upscaled image.
    """
    try:
        return _upscale_realesrgan(image, scale)
    except ImportError:
        pass

    # Fallback: high-quality LANCZOS upscale
    w, h = image.size
    return image.resize((w * scale, h * scale), Image.LANCZOS)


def _upscale_realesrgan(image: Image.Image, scale: int) -> Image.Image:
    """Upscale using Real-ESRGAN model (requires ``realesrgan`` package)."""
    import numpy as np  # noqa: F811 - lazy import
    from basicsr.archs.rrdbnet_arch import RRDBNet  # type: ignore[import-untyped]
    from realesrgan import RealESRGANer  # type: ignore[import-untyped]

    model = RRDBNet(
        num_in_ch=3, num_out_ch=3, num_feat=64,
        num_block=23, num_grow_ch=32, scale=scale,
    )
    # model_path is unused when a pre-built model is supplied directly
    upsampler = RealESRGANer(
        scale=scale, model_path="", model=model, half=False,
    )

    img_array = np.array(image)
    output, _ = upsampler.enhance(img_array, outscale=scale)
    return Image.fromarray(output)


def postprocess(
    stylized: Image.Image,
    content: Image.Image,
    *,
    harmonize: bool = True,
    harmonize_strength: float = 0.8,
    do_sharpen: bool = True,
    sharpen_radius: float = 2.0,
    sharpen_percent: int = 150,
    sharpen_threshold: int = 3,
    do_upscale: bool = False,
    upscale_factor: int = 2,
) -> Image.Image:
    """Run the full post-processing pipeline.

    Args:
        stylized: The stylized output from AdaIN style transfer.
        content: The original content image (for color reference).
        harmonize: Enable color harmonization.
        harmonize_strength: Strength of color harmonization (0.0-1.0).
        do_sharpen: Enable sharpening.
        sharpen_radius: Unsharp mask radius.
        sharpen_percent: Unsharp mask percent.
        sharpen_threshold: Unsharp mask threshold.
        do_upscale: Enable super-resolution upscaling.
        upscale_factor: Upscaling factor.

    Returns:
        Post-processed image.
    """
    result = stylized

    if harmonize:
        result = color_harmonize(result, content, strength=harmonize_strength)

    if do_sharpen:
        result = sharpen(
            result, radius=sharpen_radius,
            percent=sharpen_percent, threshold=sharpen_threshold,
        )

    if do_upscale:
        result = upscale(result, scale=upscale_factor)

    return result
