#!/bin/bash
# sign-image-metadata.sh
# Extracts metadata from an image, signs it with a GPG key, and optionally
# embeds the detached signature back into the image as a Comment field.
#
# Usage: ./scripts/sign-image-metadata.sh <image-file> [gpg-key-id]
#
# Environment variables:
#   GPG_KEY_ID   - GPG key ID or email to sign with (overridden by argument 2)
#   GPG_PASSPHRASE - Passphrase for the GPG key (used in non-interactive/CI mode)

set -euo pipefail

IMAGE_FILE="${1:-}"
KEY_ID="${2:-${GPG_KEY_ID:-}}"

if [[ -z "$IMAGE_FILE" ]]; then
  echo "Usage: $0 <image-file> [gpg-key-id]" >&2
  exit 1
fi

if [[ ! -f "$IMAGE_FILE" ]]; then
  echo "Error: file not found: $IMAGE_FILE" >&2
  exit 1
fi

# Verify required tools are available
for tool in gpg exiftool; do
  if ! command -v "$tool" &>/dev/null; then
    echo "Error: '$tool' is required but not installed." >&2
    exit 1
  fi
done

BASENAME="${IMAGE_FILE%.*}"
METADATA_FILE="${BASENAME}.metadata.json"
SIGNATURE_FILE="${BASENAME}.metadata.json.sig"

# Step 1: Extract metadata
echo "Extracting metadata from '$IMAGE_FILE'..."
exiftool -json "$IMAGE_FILE" > "$METADATA_FILE"

# Step 2: Sign metadata with GPG
echo "Signing metadata..."
GPG_SIGN_ARGS=(--batch --yes --detach-sign --output "$SIGNATURE_FILE")
if [[ -n "${GPG_PASSPHRASE:-}" ]]; then
  GPG_SIGN_ARGS+=(--passphrase-fd 0)
fi
if [[ -n "$KEY_ID" ]]; then
  GPG_SIGN_ARGS+=(--local-user "$KEY_ID")
fi
GPG_SIGN_ARGS+=("$METADATA_FILE")

if [[ -n "${GPG_PASSPHRASE:-}" ]]; then
  echo "$GPG_PASSPHRASE" | gpg "${GPG_SIGN_ARGS[@]}"
else
  gpg "${GPG_SIGN_ARGS[@]}"
fi

echo "Signature written to '$SIGNATURE_FILE'."

# Step 3: Embed the base64-encoded signature into the image Comment field
echo "Embedding signature into image metadata..."
SIGNATURE_B64=$(base64 < "$SIGNATURE_FILE" | tr -d '\n')
if [[ -z "$SIGNATURE_B64" ]]; then
  echo "Error: base64 encoding of signature produced empty output." >&2
  exit 1
fi
exiftool -overwrite_original -Comment="pgp-sig:${SIGNATURE_B64}" "$IMAGE_FILE"

echo "Done. Signature embedded in '$IMAGE_FILE'."
echo "To verify: gpg --verify '$SIGNATURE_FILE' '$METADATA_FILE'"
