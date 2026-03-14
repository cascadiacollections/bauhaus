#!/usr/bin/env bash
# Download VGG-19 encoder and AdaIN decoder weights
set -euo pipefail

WEIGHTS_DIR="$(dirname "$0")/weights"
mkdir -p "$WEIGHTS_DIR"

BASE_URL="https://github.com/naoto0804/pytorch-AdaIN/releases/download/v0.0.0"

if [[ ! -f "$WEIGHTS_DIR/vgg_normalised.pth" ]]; then
    echo "Downloading VGG-19 encoder..."
    curl -sfL -o "$WEIGHTS_DIR/vgg_normalised.pth" "$BASE_URL/vgg_normalised.pth"
fi

if [[ ! -f "$WEIGHTS_DIR/decoder.pth" ]]; then
    echo "Downloading AdaIN decoder..."
    curl -sfL -o "$WEIGHTS_DIR/decoder.pth" "$BASE_URL/decoder.pth"
fi

echo "Models ready in $WEIGHTS_DIR"
