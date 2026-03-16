"""Orchestrator: fetch CC0 art → apply AdaIN style transfer → upload to R2."""

import argparse
import json
import os
import random
import sys
from datetime import date
from io import BytesIO
from pathlib import Path

from PIL import Image
from PIL.ExifTags import TAGS as EXIF_TAGS, IFD

from fetch import fetch_artwork
from postprocess import postprocess
from quality import score_image
from stylize import StyleTransfer, gradient_alpha_mask, luminance_alpha_mask
from upload import upload

STYLES_DIR = Path(__file__).resolve().parent.parent / "styles"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

# EXIF tag IDs
_EXIF_IMAGE_DESCRIPTION = 0x010E
_EXIF_ARTIST = 0x013B
_EXIF_COPYRIGHT = 0x8298


def extract_exif(image_bytes: bytes) -> dict:
    """Extract EXIF metadata from image bytes into a plain dict.

    Returns a dict mapping human-readable tag names to their string values.
    Binary or non-serialisable values are skipped.
    """
    img = Image.open(BytesIO(image_bytes))
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
    except Exception:
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
    except Exception:
        pass

    return result


def strip_exif(image: Image.Image) -> bytes:
    """Encode image as JPEG without any EXIF metadata."""
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def embed_exif(image: Image.Image, metadata: dict) -> bytes:
    """Encode image as JPEG with EXIF metadata (title, artist, copyright)."""
    exif = image.getexif()
    exif[_EXIF_IMAGE_DESCRIPTION] = metadata.get("title", "")
    artist = metadata.get("photographer") or metadata.get("artist", "")
    exif[_EXIF_ARTIST] = artist
    license_name = metadata.get("license", "")
    license_url = metadata.get("license_url", "")
    exif[_EXIF_COPYRIGHT] = f"{license_name} — {license_url}" if license_url else license_name
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=95, exif=exif.tobytes())
    return buf.getvalue()


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


def ensure_models():
    """Download model weights if not present (cross-platform)."""
    weights_dir = Path(__file__).resolve().parent.parent / "models" / "weights"
    if (weights_dir / "vgg_normalised.pth").exists() and (weights_dir / "decoder.pth").exists():
        return

    import subprocess
    script = Path(__file__).resolve().parent.parent / "models" / "download_models.py"
    subprocess.run([sys.executable, str(script)], check=True)


def main():
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
    parser.add_argument("--max-size", type=int,
                        default=int(os.environ.get("MAX_SIZE", "1920")),
                        help="Max processing resolution in px (default: 1920, env: MAX_SIZE)")
    args = parser.parse_args()

    style_mode = os.environ.get("STYLE_MODE", "curated")
    landscapes_only = not args.any_subject and os.environ.get("LANDSCAPES_ONLY", "true").lower() != "false"

    # 1. Fetch CC0 artwork
    quality_gate = not args.skip_quality_check
    print(f"Fetching artwork from {args.source} (landscapes_only={landscapes_only})...")
    artwork = fetch_artwork(args.source, landscapes_only=landscapes_only, quality_gate=quality_gate)
    print(f"  Title: {artwork.title}")
    print(f"  Artist: {artwork.artist}")

    # 1b. Quality gate
    content_img = Image.open(BytesIO(artwork.image_bytes)).convert("RGB")
    if not args.skip_quality_check:
        qscore = score_image(content_img)
        print(f"  Quality: sharpness={qscore['sharpness']}, "
              f"{qscore['width']}×{qscore['height']}, pass={qscore['pass']}")
        if not qscore["pass"]:
            print("  ⚠ Source image failed quality check — proceeding anyway", file=sys.stderr)

    # 2. Pick style reference
    print(f"Picking style reference (mode={style_mode})...")
    style_img, style_meta = pick_style(style_mode)
    print(f"  Style: {style_meta.get('style_title', 'unknown')}")

    # 3. Download models if needed
    print("Ensuring models are downloaded...")
    ensure_models()

    # 4. Apply AdaIN style transfer
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
    print("  Style transfer complete.")

    # 5. Post-processing
    pp_enabled = args.color_harmonize or args.sharpen or args.upscale
    if pp_enabled:
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
        print("  Post-processing complete.")

    # Build metadata
    metadata = artwork.to_metadata()
    metadata.update(style_meta)
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

    # Extract EXIF/IPTC metadata from source image
    print("Extracting source EXIF metadata...")
    source_exif = extract_exif(artwork.image_bytes)
    if source_exif:
        metadata["exif"] = source_exif
        print(f"  Extracted {len(source_exif)} EXIF tags")
    else:
        print("  No EXIF metadata found in source image")

    # Embed EXIF metadata into images
    print("Embedding EXIF metadata...")
    stylized_bytes = embed_exif(stylized, metadata)
    original_img = Image.open(BytesIO(artwork.image_bytes)).convert("RGB")
    original_bytes = embed_exif(original_img, metadata)

    # Generate stripped variant (no EXIF)
    stripped_bytes = None
    if args.strip:
        print("Generating EXIF-stripped variant...")
        stripped_bytes = strip_exif(stylized)

    if args.dry_run:
        # Save locally
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        original_path = OUTPUT_DIR / "original.jpg"
        stylized_path = OUTPUT_DIR / "stylized.jpg"
        metadata_path = OUTPUT_DIR / "metadata.json"

        original_path.write_bytes(original_bytes)
        stylized_path.write_bytes(stylized_bytes)
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        print(f"\nDry run complete:")
        print(f"  Original:  {original_path}")
        print(f"  Stylized:  {stylized_path}")
        print(f"  Metadata:  {metadata_path}")

        if stripped_bytes:
            stripped_path = OUTPUT_DIR / "stylized.stripped.jpg"
            stripped_path.write_bytes(stripped_bytes)
            print(f"  Stripped:  {stripped_path}")
        return

    # 6. Upload to R2
    print("Uploading to R2...")
    keys = upload(original_bytes, stylized_bytes, metadata, stripped_bytes=stripped_bytes)
    print("Uploaded:")
    for name, key in keys.items():
        print(f"  {name}: {key}")


if __name__ == "__main__":
    main()
