"""Orchestrator: fetch CC0 art → apply AdaIN style transfer → upload to R2."""

import argparse
import json
import os
import platform
import random
import socket
import sys
import time
from datetime import date
from datetime import datetime, timezone
from io import BytesIO
from math import gcd
from pathlib import Path

from PIL import Image
from PIL.ExifTags import TAGS as EXIF_TAGS, IFD

from fetch import fetch_artwork
from postprocess import postprocess
from quality import aesthetic_score, score_image
from stylize import StyleTransfer, gradient_alpha_mask, luminance_alpha_mask
from upload import upload
from variants import generate_variants

STYLES_DIR = Path(__file__).resolve().parent.parent / "styles"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

# EXIF tag IDs
_EXIF_IMAGE_DESCRIPTION = 0x010E
_EXIF_ARTIST = 0x013B
_EXIF_COPYRIGHT = 0x8298


def _max_rss_mb() -> float | None:
    """Best-effort max RSS measurement in MiB on Unix-like systems."""
    try:
        import resource
    except ImportError:
        return None

    usage = resource.getrusage(resource.RUSAGE_SELF)
    # Linux reports KiB, macOS reports bytes.
    if sys.platform == "darwin":
        return round(usage.ru_maxrss / (1024 * 1024), 2)
    return round(usage.ru_maxrss / 1024, 2)


def _write_metrics(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def extract_exif(image_bytes: bytes) -> dict[str, str]:
    """Extract EXIF metadata from image bytes into a plain dict.

    Returns a dict mapping human-readable tag names to their string values.
    Binary or non-serialisable values are skipped.
    """
    with Image.open(BytesIO(image_bytes)) as img:
        exif_data = img.getexif()
        result: dict[str, str] = {}
        for tag_id, value in exif_data.items():
            tag_name = EXIF_TAGS.get(tag_id, str(tag_id))
            if isinstance(value, bytes):
                continue
            result[tag_name] = str(value)

        # Try to extract EXIF IFD (shutter speed, ISO, etc.)
        try:
            exif_ifd = exif_data.get_ifd(IFD.Exif)
            for tag_id, value in exif_ifd.items():
                tag_name = EXIF_TAGS.get(tag_id, str(tag_id))
                if isinstance(value, bytes):
                    continue
                result[tag_name] = str(value)
        except KeyError:
            pass

        # Try to extract GPS IFD
        try:
            from PIL.ExifTags import GPSTAGS
            gps_ifd = exif_data.get_ifd(IFD.GPSInfo)
            for tag_id, value in gps_ifd.items():
                tag_name = GPSTAGS.get(tag_id, str(tag_id))
                if isinstance(value, bytes):
                    continue
                result[f"GPS{tag_name}"] = str(value)
        except KeyError:
            pass

    return result


def strip_exif(image: Image.Image) -> bytes:
    """Encode image as JPEG without any EXIF metadata."""
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def embed_exif(image: Image.Image, metadata: dict, progressive: bool = False) -> bytes:
    """Encode image as JPEG with EXIF metadata (title, artist, copyright).

    Args:
        image: PIL Image to encode.
        metadata: Dict with title, artist/photographer, license info.
        progressive: If True, encode as progressive JPEG for faster perceived load.
    """
    exif = image.getexif()
    exif[_EXIF_IMAGE_DESCRIPTION] = metadata.get("title", "")
    artist = metadata.get("photographer") or metadata.get("artist", "")
    exif[_EXIF_ARTIST] = artist
    license_name = metadata.get("license", "")
    license_url = metadata.get("license_url", "")
    exif[_EXIF_COPYRIGHT] = f"{license_name} — {license_url}" if license_url else license_name
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=95, progressive=progressive, exif=exif.tobytes())
    return buf.getvalue()


def build_manifest(
    metadata: dict,
    width: int,
    height: int,
    stylized_size: int,
    variants: dict[str, bytes],
    date_str: str,
) -> dict:
    """Build a manifest dict listing available image variants for srcset / responsive use."""
    g = gcd(width, height) or 1
    aspect = f"{width // g}:{height // g}"

    items: list[dict] = [
        {
            "format": "jpeg",
            "width": width,
            "height": height,
            "url": f"/api/{date_str}",
            "size_bytes": stylized_size,
        },
    ]

    _FMT = {
        "avif": ("avif", f"/api/{date_str}?format=avif"),
        "webp": ("webp", f"/api/{date_str}?format=webp"),
        "progressive.jpg": ("progressive-jpeg", f"/api/{date_str}?progressive=true"),
        "stripped.jpg": ("stripped-jpeg", f"/api/{date_str}?strip=true"),
    }
    for suffix, (fmt, url) in _FMT.items():
        if suffix in variants:
            items.append({
                "format": fmt,
                "width": width,
                "height": height,
                "url": url,
                "size_bytes": len(variants[suffix]),
            })

    return {
        "date": date_str,
        "variants": items,
        "aspect_ratio": aspect,
        "license": {
            "type": metadata.get("license", ""),
            "url": metadata.get("license_url", ""),
        },
        "source": metadata.get("source", ""),
        "source_url": metadata.get("source_url", ""),
    }


def build_license_details(metadata: dict) -> dict:
    """Build a structured license object from flat metadata fields."""
    return {
        "type": metadata.get("license", ""),
        "url": metadata.get("license_url", ""),
        "source": metadata.get("source", ""),
        "source_url": metadata.get("source_url", ""),
    }


def build_variants(
    stylized_img: Image.Image,
    stylized_bytes: bytes,
    original_img: Image.Image,
    original_bytes: bytes,
    today_str: str,
) -> list[dict]:
    """Build a variants array describing available image formats and sizes."""
    return [
        {
            "type": "stylized",
            "format": "image/jpeg",
            "width": stylized_img.width,
            "height": stylized_img.height,
            "url": f"/api/{today_str}",
            "size_bytes": len(stylized_bytes),
        },
        {
            "type": "original",
            "format": "image/jpeg",
            "width": original_img.width,
            "height": original_img.height,
            "url": f"/api/{today_str}/original",
            "size_bytes": len(original_bytes),
        },
    ]


def load_styles_manifest() -> list[dict]:
    manifest = STYLES_DIR / "styles.json"
    if not manifest.exists():
        return []
    return json.loads(manifest.read_text())


def pick_style(mode: str) -> tuple[Image.Image, dict]:
    """Pick a style reference image. Returns (PIL Image, metadata dict)."""
    if mode == "random":
        # Fetch a second CC0 painting from a random source
        source = random.choice(["met", "artic"])
        artwork = fetch_artwork(source)
        img = Image.open(BytesIO(artwork.image_bytes)).convert("RGB")
        return img, {
            "style_title": artwork.title,
            "style_artist": artwork.artist,
            "style_source": artwork.source,
            "style_source_url": artwork.source_url,
        }

    # Curated mode: rotate through styles based on day of year
    styles = load_styles_manifest()
    if not styles:
        raise RuntimeError("No styles found in styles.json and STYLE_MODE=curated")

    idx = date.today().timetuple().tm_yday % len(styles)
    style_info = styles[idx]

    style_path = STYLES_DIR / style_info["filename"]
    if not style_path.exists():
        raise FileNotFoundError(f"Style image not found: {style_path}")

    img = Image.open(style_path).convert("RGB")
    return img, {
        "style_title": style_info["title"],
        "style_artist": style_info["artist"],
        "style_source_url": style_info.get("source_url", ""),
    }


def resolve_runtime_profile(max_size: int, memory_profile: str, generate_variants: bool) -> tuple[int, bool]:
    """Cap resolution and variant generation for constrained CPU/RAM runtimes."""
    if memory_profile == "low-memory":
        return min(max_size, 1024), False
    return max_size, generate_variants


def ensure_models():
    """Download model weights if not present (cross-platform)."""
    weights_dir = Path(__file__).resolve().parent.parent / "models" / "weights"
    if (weights_dir / "vgg_normalised.pth").exists() and (weights_dir / "decoder.pth").exists():
        return

    import subprocess
    script = Path(__file__).resolve().parent.parent / "models" / "download_models.py"
    subprocess.run([sys.executable, str(script)], check=True)


def main():
    run_started = time.perf_counter()
    timings: dict[str, float] = {}

    def record_timing(name: str, started_at: float) -> None:
        timings[name] = round(time.perf_counter() - started_at, 3)

    def emit_metrics(
        args: argparse.Namespace,
        metrics_path: Path | None,
        dry_run: bool,
        variants_count: int,
        uploaded_count: int,
    ) -> None:
        if not metrics_path:
            return

        payload = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "source": args.source,
            "style_mode": style_mode,
            "dry_run": dry_run,
            "max_size": args.max_size,
            "variants_enabled": args.variants,
            "timings_sec": timings,
            "total_sec": round(time.perf_counter() - run_started, 3),
            "peak_rss_mb": _max_rss_mb(),
            "variants_count": variants_count,
            "uploaded_objects": uploaded_count,
            "metrics_label": args.metrics_label or "",
        }
        _write_metrics(metrics_path, payload)
        print(f"Metrics written: {metrics_path}")

    parser = argparse.ArgumentParser(description="Generate daily stylized artwork")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and stylize locally, skip R2 upload")
    parser.add_argument("--source", default="unsplash", choices=["unsplash", "met", "artic"],
                        help="Art source (default: unsplash)")
    parser.add_argument("--alpha", type=float, default=0.8,
                        help="Style strength 0.0-1.0 (default: 0.8)")
    parser.add_argument("--alpha-mode", default="uniform",
                        choices=["uniform", "gradient", "luminance"],
                        help="Alpha blending mode (default: uniform)")
    parser.add_argument("--fg-alpha", type=float, default=0.5,
                        help="Foreground / bottom / dark region alpha (default: 0.5)")
    parser.add_argument("--bg-alpha", type=float, default=0.9,
                        help="Background / top / bright region alpha (default: 0.9)")
    parser.add_argument("--any-subject", action="store_true",
                        help="Disable landscape filter, allow any subject")
    parser.add_argument("--skip-quality-check", action="store_true",
                        help="Skip image quality scoring (sharpness, resolution, aspect ratio)")
    parser.add_argument("--color-harmonize", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="Apply color harmonization (default: on)")
    parser.add_argument("--sharpen", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="Apply sharpening (default: on)")
    parser.add_argument("--upscale", action="store_true", default=False,
                        help="Apply super-resolution upscaling (default: off)")
    parser.add_argument("--strip", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="Pre-generate EXIF-stripped image variant (default: on)")
    memory_profile = os.environ.get("MEMORY_PROFILE", "balanced")
    default_variants = os.environ.get("GENERATE_VARIANTS", "true").lower() != "false"
    if memory_profile == "low-memory":
        default_variants = False

    parser.add_argument("--memory-profile", choices=["balanced", "low-memory"],
                        default=memory_profile,
                        help="Runtime profile for constrained CPUs/RAM (default: balanced, env: MEMORY_PROFILE)")
    parser.add_argument("--max-size", type=int,
                        default=int(os.environ.get("MAX_SIZE", "1280")),
                        help="Max processing resolution in px (default: 1280, env: MAX_SIZE)")
    parser.add_argument("--variants", action=argparse.BooleanOptionalAction,
                        default=default_variants,
                        help="Generate AVIF and WebP variants (default: on, env: GENERATE_VARIANTS)")
    parser.add_argument("--metrics-out", default=os.environ.get("METRICS_OUT", ""),
                        help="Write run timing/resource metrics JSON to this file path")
    parser.add_argument("--metrics-label", default=os.environ.get("METRICS_LABEL", ""),
                        help="Optional label to annotate benchmark runs")
    args = parser.parse_args()
    metrics_path = Path(args.metrics_out).expanduser() if args.metrics_out else None

    style_mode = os.environ.get("STYLE_MODE", "curated")
    landscapes_only = not args.any_subject and os.environ.get("LANDSCAPES_ONLY", "true").lower() != "false"

    args.max_size, args.variants = resolve_runtime_profile(
        args.max_size,
        args.memory_profile,
        args.variants,
    )

    # 1. Fetch CC0 artwork
    quality_gate = not args.skip_quality_check
    t = time.perf_counter()
    print(f"Fetching artwork from {args.source} (landscapes_only={landscapes_only})...")
    artwork = fetch_artwork(args.source, landscapes_only=landscapes_only, quality_gate=quality_gate)
    record_timing("fetch_artwork", t)
    print(f"  Title: {artwork.title}")
    print(f"  Artist: {artwork.artist}")

    # 1b. Quality gate
    content_img = Image.open(BytesIO(artwork.image_bytes)).convert("RGB")
    if not args.skip_quality_check:
        t = time.perf_counter()
        qscore = score_image(content_img)
        record_timing("quality_gate", t)
        print(f"  Quality: sharpness={qscore['sharpness']}, "
              f"{qscore['width']}×{qscore['height']}, pass={qscore['pass']}")
        if not qscore["pass"]:
            print("  ⚠ Source image failed quality check — proceeding anyway", file=sys.stderr)

    # 2. Pick style reference
    t = time.perf_counter()
    print(f"Picking style reference (mode={style_mode})...")
    style_img, style_meta = pick_style(style_mode)
    record_timing("pick_style", t)
    print(f"  Style: {style_meta.get('style_title', 'unknown')}")

    # 3. Download models if needed
    t = time.perf_counter()
    print("Ensuring models are downloaded...")
    ensure_models()
    record_timing("ensure_models", t)

    # 4. Apply AdaIN style transfer
    t = time.perf_counter()
    print("Applying style transfer...")
    model = StyleTransfer()

    alpha_mask = None
    if args.alpha_mode == "gradient":
        # Use content image dimensions as proxy; mask is resized to feature
        # map size inside transfer().
        alpha_mask = gradient_alpha_mask(
            content_img.height, content_img.width,
            top_alpha=args.bg_alpha, bottom_alpha=args.fg_alpha,
        )
    elif args.alpha_mode == "luminance":
        from torchvision import transforms as T
        content_tensor = T.ToTensor()(content_img).unsqueeze(0)
        alpha_mask = luminance_alpha_mask(
            content_tensor, bright_alpha=args.bg_alpha, dark_alpha=args.fg_alpha,
        )

    stylized = model.transfer(
        content_img, style_img,
        alpha=args.alpha, alpha_mask=alpha_mask, max_size=args.max_size,
    )
    record_timing("style_transfer", t)
    print("  Style transfer complete.")

    # 5. Post-processing
    pp_enabled = args.color_harmonize or args.sharpen or args.upscale
    if pp_enabled:
        t = time.perf_counter()
        steps = []
        if args.color_harmonize:
            steps.append("color-harmonize")
        if args.sharpen:
            steps.append("sharpen")
        if args.upscale:
            steps.append("upscale")
        print(f"Post-processing ({', '.join(steps)})...")
        stylized = postprocess(
            stylized, content_img,
            harmonize=args.color_harmonize,
            do_sharpen=args.sharpen,
            do_upscale=args.upscale,
        )
        record_timing("postprocess", t)
        print("  Post-processing complete.")

    # Build metadata
    metadata = artwork.to_metadata()
    metadata.update(style_meta)
    metadata["aesthetic"] = aesthetic_score(stylized)
    metadata["alpha"] = args.alpha
    metadata["alpha_mode"] = args.alpha_mode
    if args.alpha_mode != "uniform":
        metadata["fg_alpha"] = args.fg_alpha
        metadata["bg_alpha"] = args.bg_alpha
    metadata["style_mode"] = style_mode
    metadata["postprocessing"] = {
        "color_harmonize": args.color_harmonize,
        "sharpen": args.sharpen,
        "upscale": args.upscale,
    }

    # Extract EXIF metadata from source image
    t = time.perf_counter()
    print("Extracting source EXIF metadata...")
    source_exif = extract_exif(artwork.image_bytes)
    record_timing("extract_exif", t)
    if source_exif:
        metadata["exif"] = source_exif
        print(f"  Extracted {len(source_exif)} EXIF tags")
    else:
        print("  No EXIF metadata found in source image")

    # Embed EXIF metadata into images
    t = time.perf_counter()
    print("Embedding EXIF metadata...")
    stylized_bytes = embed_exif(stylized, metadata)
    original_img = Image.open(BytesIO(artwork.image_bytes)).convert("RGB")
    original_bytes = embed_exif(original_img, metadata)
    record_timing("embed_exif", t)

    # Add structured license details and variant descriptors
    metadata["license_details"] = build_license_details(metadata)
    # Capture today once to avoid day-boundary skew between manifest and upload
    today = date.today()
    today_str = today.isoformat()
    metadata["variants"] = build_variants(
        stylized, stylized_bytes,
        original_img, original_bytes,
        today_str,
    )

    # Generate stripped variant (no EXIF)
    stripped_bytes = None
    if args.strip:
        print("Generating EXIF-stripped variant...")
        stripped_bytes = strip_exif(stylized)

    # Generate all image variants (AVIF, WebP, progressive JPEG, stripped JPEG)
    variants: dict[str, bytes] = {}
    if args.variants:
        t = time.perf_counter()
        print("Generating image variants (AVIF, WebP, progressive, stripped)...")
        exif_bytes: bytes | None = None
        try:
            with Image.open(BytesIO(stylized_bytes)) as tmp:
                exif_obj = tmp.getexif()
                if exif_obj:
                    exif_bytes = exif_obj.tobytes()
        except Exception:
            pass
        variants = generate_variants(stylized, exif_bytes=exif_bytes)
        record_timing("generate_variants", t)
        print(f"  Generated {len(variants)} variant(s): {', '.join(sorted(variants))}")

    # Build manifest with aspect_ratio and all variants
    w, h = stylized.size
    manifest = build_manifest(metadata, w, h, len(stylized_bytes), variants, today_str)

    if args.dry_run:
        # Save locally
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        original_path = OUTPUT_DIR / "original.jpg"
        stylized_path = OUTPUT_DIR / "stylized.jpg"
        metadata_path = OUTPUT_DIR / "metadata.json"
        manifest_path = OUTPUT_DIR / "manifest.json"

        original_path.write_bytes(original_bytes)
        stylized_path.write_bytes(stylized_bytes)
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        print(f"\nDry run complete:")
        print(f"  Original:  {original_path}")
        print(f"  Stylized:  {stylized_path}")
        print(f"  Metadata:  {metadata_path}")
        print(f"  Manifest:  {manifest_path}")

        for suffix, data in sorted(variants.items()):
            variant_path = OUTPUT_DIR / f"stylized.{suffix}"
            variant_path.write_bytes(data)
            print(f"  Variant:   {variant_path}")

        if stripped_bytes:
            stripped_path = OUTPUT_DIR / "stylized.stripped.jpg"
            stripped_path.write_bytes(stripped_bytes)
            print(f"  Stripped:  {stripped_path}")
        emit_metrics(
            args=args,
            metrics_path=metrics_path,
            dry_run=True,
            variants_count=len(variants),
            uploaded_count=0,
        )
        return

    # 7. Upload to R2
    t = time.perf_counter()
    print("Uploading to R2...")
    keys = upload(
        original_bytes, stylized_bytes, metadata,
        manifest=manifest,
        today=today,
        variants=variants,
        stripped_bytes=stripped_bytes,
    )
    record_timing("upload", t)
    print("Uploaded:")
    for name, key in keys.items():
        print(f"  {name}: {key}")
    emit_metrics(
        args=args,
        metrics_path=metrics_path,
        dry_run=False,
        variants_count=len(variants),
        uploaded_count=len(keys),
    )


if __name__ == "__main__":
    main()
