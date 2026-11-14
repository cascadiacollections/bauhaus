# Bauhaus — Daily Stylized Art

# Container runtime for docker-build/docker-run: Apple's `container` CLI on
# macOS, docker elsewhere. Override with CONTAINER_ENGINE=podman (etc.).
ENGINE := env("CONTAINER_ENGINE", if os() == "macos" { "container" } else { "docker" })

# Default recipe: list available commands
default:
    @just --list

# Install Python dependencies
setup:
    uv sync

# Install all dependencies (Python + Worker)
setup-all: setup
    cd worker && npm ci

# Download AdaIN + NIMA model weights (~107 MB) and convert NIMA to PyTorch layout
download-models:
    uv run --script models/download_models.py

# Download the curated CC0 style references listed in styles/styles.json
download-styles *ARGS:
    uv run --script styles/download_styles.py {{ ARGS }}

# Run tests
test:
    uv run pytest -v

# Check source imports resolve
check:
    cd src && uv run python -c "from fetch import fetch_artwork; from stylize import StyleTransfer; from upload import upload; from postprocess import postprocess; print('OK')"

# Generate locally (nothing published)
# On Apple Silicon, PyTorch MPS acceleration is used automatically.
generate *ARGS:
    uv run python src/main.py --dry-run {{ ARGS }}

# Run a reproducible generation benchmark and write metrics JSON
benchmark-generate *ARGS:
    uv run python src/main.py --dry-run --source met --metrics-out output/benchmark/metrics.json {{ ARGS }}

# Enforce benchmark thresholds against metrics JSON
benchmark-gate metrics='output/benchmark/metrics.json' max_total='210' max_style_transfer='120':
    uv run python src/benchmark_gate.py --metrics {{ metrics }} --max-total {{ max_total }} --max-style-transfer {{ max_style_transfer }}

# Build the container image with ENGINE (docker; `container` on macOS)
docker-build:
    {{ ENGINE }} build -t bauhaus .

# Run the container image (dry run) with ENGINE (docker; `container` on macOS)
docker-run *ARGS:
    {{ ENGINE }} run --rm -v "{{ justfile_directory() }}/output:/app/output" bauhaus --dry-run {{ ARGS }}

alias container-build := docker-build
alias container-run := docker-run

# Start Worker dev server
worker-dev:
    cd worker && npx wrangler dev

# Typecheck Worker
worker-check:
    cd worker && npx tsc --noEmit

# Deploy Worker to Cloudflare
worker-deploy:
    cd worker && npx wrangler deploy

