# Bauhaus — Daily Stylized Art

# Default recipe: list available commands
default:
    @just --list

# Install Python dependencies
setup:
    uv sync

# Install all dependencies (Python + Worker)
setup-all: setup
    cd worker && npm ci

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

# Build Docker image
docker-build:
    docker build -t bauhaus .

# Run Docker container
docker-run *ARGS:
    docker run --rm -v ./output:/app/output bauhaus --dry-run {{ ARGS }}

# Start Worker dev server
worker-dev:
    cd worker && npx wrangler dev

# Typecheck Worker
worker-check:
    cd worker && npx tsc --noEmit

# Deploy Worker to Cloudflare
worker-deploy:
    cd worker && npx wrangler deploy
