# Bauhaus — Daily Stylized Art

# Default recipe: list available commands
default:
    @just --list

# Install Python dependencies
setup:
    uv sync

# Install all dependencies (Python + Worker)
setup-all: setup
    cd worker && bun install --frozen-lockfile

# Download AdaIN model weights (~94 MB)
download-models:
    python models/download_models.py

# Run tests
test:
    uv run pytest -v

# Check source imports resolve
check:
    cd src && uv run python -c "from fetch import fetch_artwork; from stylize import StyleTransfer; from upload import upload; from postprocess import postprocess; print('OK')"

# Generate locally (no R2 upload)
# On Apple Silicon, PyTorch MPS acceleration is used automatically.
generate *ARGS:
    uv run python src/main.py --dry-run {{ ARGS }}

# Run a reproducible generation benchmark and write metrics JSON
benchmark-generate *ARGS:
    uv run python src/main.py --dry-run --source met --metrics-out output/benchmark/metrics.json {{ ARGS }}

# Enforce benchmark thresholds against metrics JSON
benchmark-gate metrics='output/benchmark/metrics.json' max_total='210' max_style_transfer='120':
    uv run python src/benchmark_gate.py --metrics {{ metrics }} --max-total {{ max_total }} --max-style-transfer {{ max_style_transfer }}

# Build Docker image
docker-build:
    docker build -t bauhaus .

# Run Docker container
docker-run *ARGS:
    docker run --rm -v ./output:/app/output bauhaus --dry-run {{ ARGS }}

# Start Worker dev server
worker-dev:
    cd worker && bunx wrangler dev

# Typecheck Worker
worker-check:
    cd worker && bunx tsc --noEmit

# Deploy Worker to Cloudflare
worker-deploy:
    cd worker && bunx wrangler deploy
