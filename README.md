# Bauhaus

Daily stylized art from public domain museum collections. CC0 in, CC0 out.

Fetches CC0 artwork from the [Met Museum](https://www.metmuseum.org/art/collection/search) and [Art Institute of Chicago](https://www.artic.edu/collection), applies [AdaIN](https://arxiv.org/abs/1703.06868) neural style transfer, and serves the results via API.

## How it works

1. **Fetch** — Random CC0/public domain artwork from museum APIs
2. **Stylize** — AdaIN style transfer using curated CC0 style references (Monet, Hokusai, Van Gogh, etc.)
3. **Upload** — Original + stylized + metadata → Cloudflare R2
4. **Serve** — Cloudflare Worker API

Runs daily via GitHub Actions. Total cost: **$0/month**.

## API

```
GET /api/today           → today's stylized image
GET /api/today.json      → today's metadata (title, artist, source, license)
GET /api/YYYY-MM-DD      → stylized image for a specific date
GET /api/YYYY-MM-DD/original  → original unstylized image
GET /api/YYYY-MM-DD.json → metadata for a specific date
```

## Local development

```bash
# Install dependencies
pip install -r requirements.txt

# Download AdaIN model weights (~107MB)
bash models/download_models.sh

# Generate locally (no R2 upload)
python src/main.py --dry-run

# Generate from Art Institute of Chicago
python src/main.py --dry-run --source artic

# Adjust style strength (0.0-1.0)
python src/main.py --dry-run --alpha 0.6
```

### Worker

```bash
cd worker
npm install
npx wrangler dev
```

## Configuration

| Environment Variable | Description |
|---------------------|-------------|
| `R2_ENDPOINT` | Cloudflare R2 S3-compatible endpoint |
| `R2_ACCESS_KEY_ID` | R2 access key |
| `R2_SECRET_ACCESS_KEY` | R2 secret key |
| `R2_BUCKET` | R2 bucket name (default: `bauhaus`) |
| `STYLE_MODE` | `curated` (rotate through shipped styles) or `random` (fetch a second CC0 painting) |

## Licensing

| Component | License |
|-----------|---------|
| Code | MIT |
| Input art | CC0 (Met Museum, AIC public domain collections) |
| Style references | CC0 (same sources) |
| AdaIN model code | MIT ([naoto0804/pytorch-AdaIN](https://github.com/naoto0804/pytorch-AdaIN)) |
| VGG-19 encoder weights | BSD-like (torchvision) |
| **Output images** | **CC0-1.0** |
