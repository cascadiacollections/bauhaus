# Bauhaus

[![Generate Daily Art](https://github.com/cascadiacollections/bauhaus/actions/workflows/generate.yml/badge.svg)](https://github.com/cascadiacollections/bauhaus/actions/workflows/generate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Output: CC0-1.0](https://img.shields.io/badge/Output-CC0--1.0-brightgreen.svg)](https://creativecommons.org/publicdomain/zero/1.0/)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776ab.svg)](https://python.org)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

Daily stylized art from public domain museum collections. CC0 in, CC0 out.

Fetches CC0 landscapes and seascapes from the [Metropolitan Museum of Art](https://www.metmuseum.org/art/collection/search) and [Art Institute of Chicago](https://www.artic.edu/collection), applies [AdaIN](https://arxiv.org/abs/1703.06868) neural style transfer with curated style references, and serves the results via a free Cloudflare Worker API.

## Today's artwork

```bash
curl https://bauhaus.cascadiacollections.workers.dev/api/today -o wallpaper.jpg
```

## How it works

```
GitHub Actions (daily, 3 PM UTC)
  1. Fetch random CC0 landscape from Met/AIC APIs
  2. Pick curated style ref (Monet, Hokusai, Cezanne, Turner, ...)
  3. AdaIN style transfer (CPU, ~5s at native resolution)
  4. Upload original + stylized + metadata to Cloudflare R2
         |
  CF Worker API <-- R2 bucket
    GET /api/today      -> stylized image
    GET /api/today.json -> metadata
    GET /api/:date      -> archive
```

Runs daily via GitHub Actions. Total cost: **$0/month**.

| Component | Monthly cost |
|-----------|-------------|
| Cloudflare R2 (10 GB free) | $0 |
| Cloudflare Workers (100k req/day free) | $0 |
| GitHub Actions (public repo) | $0 |

## API

Base URL: `https://bauhaus.cascadiacollections.workers.dev`

| Endpoint | Returns |
|----------|---------|
| `GET /api/today` | Today's stylized image |
| `GET /api/today.json` | Today's metadata (title, artist, source, license) |
| `GET /api/YYYY-MM-DD` | Stylized image for a specific date |
| `GET /api/YYYY-MM-DD/original` | Original unstylized image |
| `GET /api/YYYY-MM-DD.json` | Metadata for a specific date |

## Local development

Requires [uv](https://github.com/astral-sh/uv) and Python 3.12+.

```bash
# Install dependencies
uv sync

# Download AdaIN model weights (~94 MB)
bash models/download_models.sh

# Generate locally (no R2 upload)
uv run python src/main.py --dry-run

# Options
uv run python src/main.py --dry-run --source artic   # Art Institute of Chicago
uv run python src/main.py --dry-run --source met      # Metropolitan Museum (default)
uv run python src/main.py --dry-run --alpha 0.5       # subtle style (0.0-1.0)
```

### Docker

```bash
docker build -t bauhaus .
docker run --rm -v ./output:/app/output bauhaus --dry-run
```

### Worker

```bash
cd worker
npm install
npx wrangler dev
```

## Configuration

| Variable | Description |
|----------|-------------|
| `R2_ENDPOINT` | Cloudflare R2 S3-compatible endpoint |
| `R2_ACCESS_KEY_ID` | R2 access key |
| `R2_SECRET_ACCESS_KEY` | R2 secret key |
| `R2_BUCKET` | Bucket name (default: `bauhaus`) |
| `STYLE_MODE` | `curated` (rotate shipped styles) or `random` (fetch second CC0 painting) |

## Style references

10 curated CC0 paintings shipped in `styles/`, spanning Impressionism, Post-Impressionism, Japonisme, and Pointillism:

Monet, Hokusai, Cezanne, Turner, Hiroshige, Seurat, Degas, Klimt, Van Gogh, Gauguin

## Licensing

| Component | License |
|-----------|---------|
| Code | MIT |
| Input art | CC0 (Met Museum, AIC public domain collections) |
| Style references | CC0 (same sources) |
| AdaIN model | MIT ([naoto0804/pytorch-AdaIN](https://github.com/naoto0804/pytorch-AdaIN)) |
| VGG-19 encoder | BSD-like (torchvision) |
| **Output images** | **CC0-1.0** |
