"""Upload artwork and metadata to Cloudflare R2 via S3-compatible API."""

import json
import os
from datetime import UTC, date, datetime
from io import BytesIO

import boto3


def _get_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


_VARIANT_CONTENT_TYPES: dict[str, str] = {
    "avif": "image/avif",
    "webp": "image/webp",
}


def upload(
    original_bytes: bytes,
    stylized_bytes: bytes,
    metadata: dict,
    bucket: str | None = None,
    today: date | None = None,
    variants: dict[str, bytes] | None = None,
    stripped_bytes: bytes | None = None,
) -> dict[str, str]:
    """Upload original, stylized, variants, and metadata to R2. Returns dict of uploaded keys."""
    bucket = bucket or os.environ.get("R2_BUCKET", "bauhaus")
    today = today or date.today()
    date_path = today.strftime("%Y/%m/%d")

    client = _get_client()
    keys = {}

    # Original image
    key = f"originals/{date_path}.jpg"
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=original_bytes,
        ContentType="image/jpeg",
        CacheControl="public, max-age=31536000, immutable",
    )
    keys["original"] = key

    # Stylized image
    key = f"stylized/{date_path}.jpg"
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=stylized_bytes,
        ContentType="image/jpeg",
        CacheControl="public, max-age=31536000, immutable",
    )
    keys["stylized"] = key

    # Stylized image variants (AVIF, WebP)
    for ext, data in (variants or {}).items():
        key = f"stylized/{date_path}.{ext}"
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=_VARIANT_CONTENT_TYPES.get(ext, f"image/{ext}"),
            CacheControl="public, max-age=31536000, immutable",
        )
        keys[f"stylized_{ext}"] = key

    # Metadata JSON
    metadata["date"] = today.isoformat()
    metadata["generated_at"] = datetime.now(UTC).isoformat()
    key = f"metadata/{date_path}.json"
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(metadata, indent=2).encode(),
        ContentType="application/json",
        CacheControl="public, max-age=31536000, immutable",
    )
    keys["metadata"] = key

    # Stripped variant (no EXIF)
    if stripped_bytes is not None:
        key = f"stylized/{date_path}.stripped.jpg"
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=stripped_bytes,
            ContentType="image/jpeg",
            CacheControl="public, max-age=31536000, immutable",
        )
        keys["stripped"] = key

    # Update latest pointer (short cache)
    client.put_object(
        Bucket=bucket,
        Key="latest.json",
        Body=json.dumps({"date": today.isoformat()}).encode(),
        ContentType="application/json",
        CacheControl="public, max-age=300",
    )
    keys["latest"] = "latest.json"

    return keys
