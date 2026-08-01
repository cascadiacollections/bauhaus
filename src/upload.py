"""Upload artwork and metadata to Cloudflare R2 via S3-compatible API."""

import json
import os
from datetime import UTC, date, datetime
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

# Content types for all supported image variant suffixes.
_VARIANT_CONTENT_TYPES: dict[str, str] = {
    "avif": "image/avif",
    "webp": "image/webp",
    "progressive.jpg": "image/jpeg",
    "stripped.jpg": "image/jpeg",
}

# Date-keyed objects are published once and never rewritten, so they are safe to
# cache indefinitely. This string must stay byte-identical to IMMUTABLE_CACHE in
# worker/src/index.ts: the Worker prefers the header stored on the R2 object and
# only falls back to its own constant, so a mismatch here is what actually gets
# served, and the Worker's value becomes unreachable for pipeline-written keys.
IMMUTABLE_CACHE = "public, max-age=31536000, s-maxage=31536000, immutable"

# latest.json is a pointer that changes daily — short cache, revalidate often.
LATEST_CACHE = "public, max-age=300"


class AlreadyPublishedError(RuntimeError):
    """Raised when a date already has published objects and overwrite is off."""


def utc_today() -> date:
    """Today's date in UTC.

    The publish date must never depend on the runner's local timezone. Hosted
    runners are UTC, but generate-self-hosted.yml runs this same code on a
    Pacific-time Mac Mini: a PT-evening dispatch (including 8 PM PT, the time
    the README advertises) would compute *yesterday's* date there, overwrite
    the previous day's keys, and rewind latest.json.
    """
    return datetime.now(UTC).date()


@lru_cache(maxsize=1)
def _get_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def prepare_metadata_for_upload(
    metadata: dict,
    today: date | None = None,
    generated_at: datetime | None = None,
) -> dict:
    """Return metadata augmented with stable upload-time fields."""
    today = today or utc_today()
    prepared = dict(metadata)
    prepared.setdefault("date", today.isoformat())
    prepared.setdefault("generated_at", (generated_at or datetime.now(UTC)).isoformat())
    return prepared


def serialize_metadata(metadata: dict) -> bytes:
    """Serialize metadata with canonical ordering for signing + upload."""
    return json.dumps(metadata, indent=2, sort_keys=True).encode()


def _assert_unpublished(client, bucket: str, date_path: str, today: date) -> None:
    """Refuse to overwrite a date that has already been published.

    Every date-keyed object is served with `immutable` and a one-year TTL, which
    is a promise that the bytes behind that URL never change. Re-running a date
    breaks that promise in a way no cache ever recovers from: uploads land
    key-by-key rather than atomically, so a client that fetched during the
    rewrite can hold the image from one artwork alongside the metadata of
    another — permanently, because `immutable` tells it never to revalidate.

    Publishing is therefore write-once by default. Pass overwrite=True
    (`--overwrite`, or the workflow_dispatch input) to republish a date
    deliberately, accepting that already-cached copies stay stale.
    """
    key = f"metadata/{date_path}.json"
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        # 404/NoSuchKey is the expected path: nothing published for this date.
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = exc.response.get("Error", {}).get("Code")
        if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
            return
        raise

    raise AlreadyPublishedError(
        f"{today.isoformat()} is already published (found {key}). "
        "Date keys are served immutable, so rewriting them leaves stale and "
        "possibly mismatched copies in caches forever. Re-run with --overwrite "
        "if that is what you want."
    )


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
    overwrite: bool = False,
) -> dict[str, str]:
    """Upload original, stylized, variants, manifest, and metadata to R2. Returns dict of uploaded keys.

    Raises AlreadyPublishedError when ``today`` has already been published and
    ``overwrite`` is False — see _assert_unpublished().
    """
    bucket = bucket or os.environ.get("R2_BUCKET", "bauhaus")
    today = today or utc_today()
    date_path = today.strftime("%Y/%m/%d")

    client = _get_client()

    if not overwrite:
        _assert_unpublished(client, bucket, date_path, today)

    keys = {}

    # Original image
    key = f"originals/{date_path}.jpg"
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=original_bytes,
        ContentType="image/jpeg",
        CacheControl=IMMUTABLE_CACHE,
    )
    keys["original"] = key

    # Stylized image
    key = f"stylized/{date_path}.jpg"
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=stylized_bytes,
        ContentType="image/jpeg",
        CacheControl=IMMUTABLE_CACHE,
    )
    keys["stylized"] = key

    # Image variants (AVIF, WebP, progressive, stripped).
    #
    # generate_variants() always emits a "stripped.jpg" entry, and main.py also
    # builds stripped_bytes independently when --strip is on (both default on).
    # Both used to be written to stylized/<date>.stripped.jpg, so every run did
    # a redundant encode and a duplicate PUT to a key it had just written.
    # An explicit stripped_bytes wins; the variant copy is skipped.
    if variants:
        skip = {"stripped.jpg"} if stripped_bytes is not None else set()
        for suffix, data in variants.items():
            if suffix in skip:
                continue
            key = f"stylized/{date_path}.{suffix}"
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=_VARIANT_CONTENT_TYPES.get(suffix, f"image/{suffix.split('.')[-1]}"),
                CacheControl=IMMUTABLE_CACHE,
            )
            keys[f"stylized_{suffix.replace('.', '_')}"] = key

    # Metadata JSON
    prepared_metadata = prepare_metadata_for_upload(metadata, today=today)
    metadata_bytes = serialize_metadata(prepared_metadata)
    key = f"metadata/{date_path}.json"
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=metadata_bytes,
        ContentType="application/json",
        CacheControl=IMMUTABLE_CACHE,
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
            CacheControl=IMMUTABLE_CACHE,
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
            CacheControl=IMMUTABLE_CACHE,
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
            CacheControl=IMMUTABLE_CACHE,
        )
        keys["stripped"] = key

    # Update latest pointer (short cache)
    client.put_object(
        Bucket=bucket,
        Key="latest.json",
        Body=json.dumps({"date": today.isoformat()}).encode(),
        ContentType="application/json",
        CacheControl=LATEST_CACHE,
    )
    keys["latest"] = "latest.json"

    return keys
