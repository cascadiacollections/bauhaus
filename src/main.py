"""Orchestrator: fetch CC0 art → apply AdaIN style transfer → upload to R2."""

import argparse
import json
import os
import random
import subprocess
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

from fetch import fetch_artwork
from stylize import StyleTransfer
from upload import upload

STYLES_DIR = Path(__file__).resolve().parent.parent / "styles"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def load_styles_manifest() -> list[dict]:
    manifest = STYLES_DIR / "styles.json"
    if not manifest.exists():
        return []
    with open(manifest) as f:
        return json.load(f)


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

    from datetime import date
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
    """Download model weights if not present."""
    weights_dir = Path(__file__).resolve().parent.parent / "models" / "weights"
    if (weights_dir / "vgg_normalised.pth").exists() and (weights_dir / "decoder.pth").exists():
        return
    script = Path(__file__).resolve().parent.parent / "models" / "download_models.sh"
    subprocess.run(["bash", str(script)], check=True)


def main():
    parser = argparse.ArgumentParser(description="Generate daily stylized artwork")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and stylize locally, skip R2 upload")
    parser.add_argument("--source", default="met", choices=["met", "artic"],
                        help="Art source (default: met)")
    parser.add_argument("--alpha", type=float, default=0.8,
                        help="Style strength 0.0-1.0 (default: 0.8)")
    parser.add_argument("--any-subject", action="store_true",
                        help="Disable landscape filter, allow any subject")
    args = parser.parse_args()

    style_mode = os.environ.get("STYLE_MODE", "curated")
    landscapes_only = not args.any_subject and os.environ.get("LANDSCAPES_ONLY", "true").lower() != "false"

    # 1. Fetch CC0 artwork
    print(f"Fetching artwork from {args.source} (landscapes_only={landscapes_only})...")
    artwork = fetch_artwork(args.source, landscapes_only=landscapes_only)
    print(f"  Title: {artwork.title}")
    print(f"  Artist: {artwork.artist}")

    # 2. Pick style reference
    print(f"Picking style reference (mode={style_mode})...")
    style_img, style_meta = pick_style(style_mode)
    print(f"  Style: {style_meta.get('style_title', 'unknown')}")

    # 3. Download models if needed
    print("Ensuring models are downloaded...")
    ensure_models()

    # 4. Apply AdaIN style transfer
    print("Applying style transfer...")
    content_img = Image.open(BytesIO(artwork.image_bytes)).convert("RGB")
    model = StyleTransfer()
    stylized = model.transfer(content_img, style_img, alpha=args.alpha)
    print("  Style transfer complete.")

    # Convert stylized to bytes
    buf = BytesIO()
    stylized.save(buf, format="JPEG", quality=95)
    stylized_bytes = buf.getvalue()

    # Build metadata
    metadata = artwork.to_metadata()
    metadata.update(style_meta)
    metadata["alpha"] = args.alpha
    metadata["style_mode"] = style_mode

    if args.dry_run:
        # Save locally
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        original_path = OUTPUT_DIR / "original.jpg"
        stylized_path = OUTPUT_DIR / "stylized.jpg"
        metadata_path = OUTPUT_DIR / "metadata.json"

        with open(original_path, "wb") as f:
            f.write(artwork.image_bytes)
        stylized.save(stylized_path, quality=95)
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        print(f"\nDry run complete:")
        print(f"  Original:  {original_path}")
        print(f"  Stylized:  {stylized_path}")
        print(f"  Metadata:  {metadata_path}")
        return

    # 5. Upload to R2
    print("Uploading to R2...")
    keys = upload(artwork.image_bytes, stylized_bytes, metadata)
    print("Uploaded:")
    for name, key in keys.items():
        print(f"  {name}: {key}")


if __name__ == "__main__":
    main()
