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
    manifest: dict | None = None,
    bucket: str | None = None,
    today: date | None = None,
    variants: dict[str, bytes] | None = None,
    stripped_bytes: bytes | None = None,
    metadata_sig: bytes | None = None,
) -> dict[str, str]:
    """Upload original, stylized, variants, manifest, and metadata to R2. Returns dict of uploaded keys."""
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

    # Image variants (AVIF, WebP, progressive, stripped)
    _all_variant_types = {
        "avif": "image/avif",
        "webp": "image/webp",
        "progressive.jpg": "image/jpeg",
        "stripped.jpg": "image/jpeg",
    }
    if variants:
        for suffix, data in variants.items():
            key = f"stylized/{date_path}.{suffix}"
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=_all_variant_types.get(suffix, _VARIANT_CONTENT_TYPES.get(suffix, f"image/{suffix.split('.')[-1]}")),
                CacheControl="public, max-age=31536000, immutable",
            )
            keys[f"stylized_{suffix.replace('.', '_')}"] = key

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

    # Metadata signature (detached GPG signature for the metadata JSON)
    if metadata_sig is not None:
        key = f"metadata/{date_path}.json.sig"
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=metadata_sig,
            ContentType="application/pgp-signature",
            CacheControl="public, max-age=31536000, immutable",
        )
        keys["metadata_sig"] = key

    # Manifest JSON (responsive variants)
    if manifest is not None:
        key = f"manifests/{date_path}.json"
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(manifest, indent=2).encode(),
            ContentType="application/json",
            CacheControl="public, max-age=31536000, immutable",
        )
        keys["manifest"] = key

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
